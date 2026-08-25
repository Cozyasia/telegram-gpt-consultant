# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio, copy, json, logging, re, threading
from telegram.ext import CommandHandler
import mtproto_auth as auth

log = logging.getLogger("mtproto-premium-big")
LOT_IDS={"Л":"5474517911374668774","О":"5449645429346020359","Т":"5442819107110004737","№":"5256029914255076855"}
DIGIT_IDS={"0":"5393480373944459905","1":"5382322671679708881","2":"5381990043642502553","3":"5381879959335738545","4":"5382054253403577563","5":"5391197405553107640","6":"5390966190283694453","7":"5382132232829804982","8":"5391038994274329680","9":"5391234698754138414","-":"5382261056078881010"}
CTA_IDS={"О":"5449645429346020359","С":"5463032576119679082","Т":"5442819107110004737","А":"5442667851246742007","В":"5449413294953606262","И":"5449768699202381205","Ь":"5472419270094760054","З":"5472327074326786286","Я":"5204256643302303428","К":"5456289915551622074","У":"5188633966051076002"}
DESC_ID="5474587738952975936"; RIGHT_ID="5471978009449731768"; LEFT_ID="5469735272017043817"
_DAEMON_LOCK=threading.Lock(); _DAEMON_STARTED=False

def _u16(s): return len((s or "").encode("utf-16-le"))//2

def _py(text, off):
    used=0
    for i,ch in enumerate(text):
        if used>=off:return i
        used+=_u16(ch)
    return len(text)

def _lot_parts(lot):
    from telethon.tl.types import MessageEntityCustomEmoji
    text=""; ents=[]
    def add(cid,fallback):
        nonlocal text
        off=_u16(text); text+=fallback; ents.append(MessageEntityCustomEmoji(offset=off,length=_u16(fallback),document_id=int(cid)))
    for ch in "ЛОТ": add(LOT_IDS[ch],"🔤")
    text+=" "; add(LOT_IDS["№"],"🔤"); text+=" "
    for ch in str(lot or "").strip():
        key="-" if ch in {"-","–","—"} else ch; cid=DIGIT_IDS.get(key)
        if cid:add(cid,"➖" if key=="-" else key+"\ufe0f\u20e3")
        else:text+=ch
    return text,ents

def _cta_parts():
    from telethon.tl.types import MessageEntityCustomEmoji
    text=""; ents=[]
    def add(cid):
        nonlocal text
        off=_u16(text); text+="🔤"; ents.append(MessageEntityCustomEmoji(offset=off,length=_u16("🔤"),document_id=int(cid)))
    for ch in "ОСТАВИТЬ":add(CTA_IDS[ch])
    text+="\n"
    for ch in "ЗАЯВКУ":add(CTA_IDS[ch])
    return text,ents

def _intersects(a0,a1,b0,b1): return max(a0,b0)<min(a1,b1)

def _replace(text,old_entities,ops):
    ops=sorted(ops,key=lambda x:x["start"]); out=[]; cursor=0; delta=0; starts={}
    for i,op in enumerate(ops):
        out.append(text[cursor:op["start"]]); starts[i]=op["start"]+delta; out.append(op["text"])
        delta+=len(op["text"])-(op["end"]-op["start"]); cursor=op["end"]
    out.append(text[cursor:]); new_text="".join(out); new=[]
    for ent in old_entities or []:
        try:s=_py(text,int(ent.offset)); e=_py(text,int(ent.offset)+int(ent.length))
        except Exception:continue
        if any(_intersects(s,e,op["start"],op["end"]) for op in ops):continue
        shift=sum(len(op["text"])-(op["end"]-op["start"]) for op in ops if op["end"]<=s)
        c=copy.copy(ent); c.offset=_u16(new_text[:s+shift]); c.length=_u16(new_text[s+shift:e+shift]); new.append(c)
    for i,op in enumerate(ops):
        base=_u16(new_text[:starts[i]])
        for ent in op.get("entities") or []:
            c=copy.copy(ent); c.offset=base+int(ent.offset); new.append(c)
    new.sort(key=lambda x:(int(x.offset),int(x.length))); return new_text,new

def upgrade_text(text,entities,lot):
    if not text or "ЛОТ №" not in text:return text,list(entities or []),False
    top=re.search(r"^🏡\s*ЛОТ\s*№[^\n]*",text,re.I); cta=re.search(r"^📝\s*ОСТАВИТЬ\s+ЗАЯВКУ\s*$",text,re.I|re.M)
    if not top or not cta:return text,list(entities or []),False
    lt,le=_lot_parts(lot); ct,ce=_cta_parts()
    new_text,new=_replace(text,entities,[{"start":top.start(),"end":top.end(),"text":lt,"entities":le},{"start":cta.start(),"end":cta.end(),"text":ct,"entities":ce}])
    from telethon.tl.types import MessageEntityCustomEmoji
    for needle,cid in (("💬",DESC_ID),("👉",RIGHT_ID),("👈",LEFT_ID)):
        pos=new_text.find(needle)
        if pos>=0:new.append(MessageEntityCustomEmoji(offset=_u16(new_text[:pos]),length=_u16(needle),document_id=int(cid)))
    new.sort(key=lambda x:(int(x.offset),int(x.length))); return new_text,new,True

def rows(catalog):
    out=[]; seen=set()
    for row in catalog.load_catalog_rows():
        lot=str(row.get("lot_id") or "").strip(); mid=str(row.get("telegram_message_id") or "").strip()
        if not lot or not mid.isdigit():continue
        i=int(mid)
        if i in seen:continue
        seen.add(i); out.append((i,lot))
    return sorted(out)

async def upgrade_one(catalog,mid,lot):
    from telethon.errors import FloodWaitError
    client=await auth.new_client(catalog)
    if not client:raise RuntimeError("MTProto session is not authorized")
    try:
        channel=await client.get_entity(catalog.CATALOG_CHANNEL); msg=await client.get_messages(channel,ids=mid)
        if not msg or not getattr(msg,"message",None):return {"result":"missing","message_id":mid,"lot":lot}
        first=msg.message.find("\n"); top_u16=_u16(msg.message[:first if first>=0 else len(msg.message)])
        if any(type(e).__name__=="MessageEntityCustomEmoji" and int(e.offset)<top_u16 for e in (msg.entities or [])):return {"result":"already","message_id":mid,"lot":lot}
        text,ents,changed=upgrade_text(msg.message,msg.entities or [],lot)
        if not changed:return {"result":"not_v7","message_id":mid,"lot":lot}
        while True:
            try:await client.edit_message(channel,mid,text,formatting_entities=ents,link_preview=False);break
            except FloodWaitError as e:await asyncio.sleep(int(e.seconds)+1)
        return {"result":"edited","message_id":mid,"lot":lot}
    finally:await client.disconnect()

async def bulk(catalog,notify=None):
    from telethon.errors import FloodWaitError
    client=await auth.new_client(catalog)
    if not client:raise RuntimeError("MTProto session is not authorized")
    edited=already=skipped=errors=0
    try:
        channel=await client.get_entity(catalog.CATALOG_CHANNEL); items=await asyncio.to_thread(rows,catalog); total=len(items)
        for idx,(mid,lot) in enumerate(items,1):
            try:
                msg=await client.get_messages(channel,ids=mid)
                if not msg or not getattr(msg,"message",None):skipped+=1;continue
                first=msg.message.find("\n"); top_u16=_u16(msg.message[:first if first>=0 else len(msg.message)])
                if any(type(e).__name__=="MessageEntityCustomEmoji" and int(e.offset)<top_u16 for e in (msg.entities or [])):already+=1;continue
                text,ents,changed=upgrade_text(msg.message,msg.entities or [],lot)
                if not changed:skipped+=1;continue
                while True:
                    try:await client.edit_message(channel,mid,text,formatting_entities=ents,link_preview=False);edited+=1;break
                    except FloodWaitError as e:await asyncio.sleep(int(e.seconds)+1)
                await asyncio.sleep(1.5)
            except Exception:errors+=1;log.exception("Premium upgrade failed mid=%s lot=%s",mid,lot)
            if notify and (idx%10==0 or idx==total):
                try:await notify.reply_text(f"Premium MTProto: {idx}/{total} · изменено {edited} · уже готово {already} · пропущено {skipped} · ошибок {errors}")
                except Exception:pass
        return {"total":total,"edited":edited,"already":already,"skipped":skipped,"errors":errors}
    finally:await client.disconnect()

async def cmd_test(update,context,catalog):
    if not auth.admin_ok(update):return
    items=await asyncio.to_thread(rows,catalog)
    if not items:await update.effective_message.reply_text("В каталоге нет лотов с message_id");return
    target=None
    if context.args:
        wanted=context.args[0].strip().lower()
        target=next(((m,l) for m,l in items if l.lower()==wanted),None)
    target=target or items[-1]
    try:await update.effective_message.reply_text(f"MTProto test: {json.dumps(await upgrade_one(catalog,*target),ensure_ascii=False)}")
    except Exception as e:log.exception("Premium test failed");await update.effective_message.reply_text(f"MTProto test error: {type(e).__name__}: {e}")

async def cmd_backfill(update,context,catalog):
    if not auth.admin_ok(update):return
    await update.effective_message.reply_text("Запускаю Premium MTProto-проход по @samuirental. Прогресс — каждые 10 постов.")
    async def run():
        try:await update.effective_message.reply_text(f"✅ Premium MTProto завершён: {json.dumps(await bulk(catalog,update.effective_message),ensure_ascii=False)}")
        except Exception as e:log.exception("Backfill failed");await update.effective_message.reply_text(f"Premium MTProto error: {type(e).__name__}: {e}")
    context.application.create_task(run())

def install(app,catalog):
    auth.install(app,catalog,ensure_daemon_started)
    app.add_handler(CommandHandler("premium_test",lambda u,c:cmd_test(u,c,catalog)),group=-90)
    app.add_handler(CommandHandler("premium_backfill",lambda u,c:cmd_backfill(u,c,catalog)),group=-90)
    ensure_daemon_started(catalog)

def ensure_daemon_started(catalog):
    global _DAEMON_STARTED
    with _DAEMON_LOCK:
        if _DAEMON_STARTED or not auth.configured() or not auth.load_session(catalog):return
        _DAEMON_STARTED=True
    def runner():
        async def mainloop():
            from telethon import TelegramClient,events
            from telethon.sessions import StringSession
            session=await asyncio.to_thread(auth.load_session,catalog); client=TelegramClient(StringSession(session),int(auth.API_ID),auth.API_HASH); await client.start(); channel=await client.get_entity(catalog.CATALOG_CHANNEL)
            async def maybe(event):
                await asyncio.sleep(8)
                try:
                    msg=await client.get_messages(channel,ids=event.message.id)
                    if not msg or not msg.message or "🏡 ЛОТ №" not in msg.message:return
                    first=msg.message.find("\n"); top_u16=_u16(msg.message[:first if first>=0 else len(msg.message)])
                    if any(type(e).__name__=="MessageEntityCustomEmoji" and int(e.offset)<top_u16 for e in (msg.entities or [])):return
                    m=re.search(r"ЛОТ\s*№\s*([A-Za-z0-9-]+)",msg.message,re.I)
                    if not m:return
                    text,ents,changed=upgrade_text(msg.message,msg.entities or [],m.group(1))
                    if changed:await client.edit_message(channel,msg.id,text,formatting_entities=ents,link_preview=False)
                except Exception:log.exception("Automatic MTProto upgrade failed")
            client.add_event_handler(maybe,events.NewMessage(chats=channel)); client.add_event_handler(maybe,events.MessageEdited(chats=channel)); await client.run_until_disconnected()
        loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        try:loop.run_until_complete(mainloop())
        except Exception:log.exception("MTProto daemon stopped")
        finally:loop.close()
    threading.Thread(target=runner,name="mtproto-premium-big",daemon=True).start()
