from __future__ import annotations
import hashlib, json, os, time
from datetime import datetime, timezone
from pathlib import Path
from .video import ffprobe_json
from .logging_utils import log

STAGES=[
    "TOPIC_SELECTED","OUTLINE_COMPLETE","INTRO_COMPLETE","CHAPTERS_COMPLETE",
    "SCRIPT_COMPLETE","TTS_COMPLETE","AUDIO_COMPLETE","VIDEO_COMPLETE",
    "THUMBNAIL_COMPLETE","METADATA_COMPLETE","UPLOAD_STARTED","UPLOAD_VERIFIED","SUCCESS"
]

def utc_now(): return datetime.now(timezone.utc).isoformat()

def sha256(path: Path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def artifact_record(path:Path, kind="file", provider=None, model=None, extra=None):
    rec={"path":str(path).replace("\\","/"),"kind":kind,"size":path.stat().st_size,"sha256":sha256(path),"verified_at":utc_now()}
    if provider:rec["provider"]=provider
    if model:rec["model"]=model
    if extra:rec.update(extra)
    return rec

def default(book_id,topic):
    return {
        "schema_version":2,"book_id":book_id,"topic":topic,"status":"QUEUED","current_stage":"TOPIC_SELECTED",
        "last_completed_chapter":-1,"chapter_count":0,"completed_chunks":[],"total_chunks":0,
        "tts_chunks":{},"artifacts":{},"youtube":{"status":"NOT_STARTED","video_id":None,"uploaded_at":None,"upload_key":None},
        "attempts":{},"last_error":None,"retryable":True,"created_at":utc_now(),"updated_at":utc_now()
    }

def local_write(path,st):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(st,indent=2,ensure_ascii=False),encoding="utf-8")
    os.replace(tmp,path)

def persist(local_state,drive,st):
    st["updated_at"]=utc_now()
    local_write(local_state,st)
    bid=st["book_id"]
    drive.put_json(st,f"WORK/{bid}/state.json")
    drive.put_json(st,f"STATE/{bid}.json")

def load_drive(drive,bid):
    try:
        return drive.download_json(f"WORK/{bid}/state.json")
    except FileNotFoundError:
        try:
            return drive.download_json(f"STATE/{bid}.json")
        except FileNotFoundError:
            return None

def record_artifact(st, relpath, localpath, kind="file", provider=None, model=None, extra=None):
    p=Path(localpath)
    if not p.exists(): raise FileNotFoundError(localpath)
    st["artifacts"][relpath]=artifact_record(p,kind,provider,model,extra)

def artifact_valid(st,relpath,localpath,min_bytes=1,media=False,expected=None):
    p=Path(localpath)
    rec=st.get("artifacts",{}).get(relpath)
    if not p.exists() or p.stat().st_size < min_bytes:return False
    if rec and rec.get("size")!=p.stat().st_size:return False
    if rec and rec.get("sha256") and sha256(p)!=rec["sha256"]:return False
    if rec and rec.get("drive_md5"):
        import hashlib
        h=hashlib.md5()
        with p.open("rb") as fh:
            for chunk in iter(lambda:fh.read(1024*1024),b""): h.update(chunk)
        if h.hexdigest()!=rec["drive_md5"]: return False
    if media:
        try:
            info=ffprobe_json(p)
            if not info.get("format"):return False
            if expected:
                for k,v in expected.items():
                    if str(info.get(k))!=str(v):return False
        except Exception:return False
    return True

def set_stage(st,stage,status=None):
    st["current_stage"]=stage
    if status:st["status"]=status

def failure(st,stage,exc,retryable=True):
    rec={"message":str(exc)[:2000],"timestamp":utc_now()}
    if hasattr(exc,"category"): rec["category"]=exc.category
    st["current_stage"]=stage; st["last_error"]=rec
    st["retryable"]=retryable; st["status"]="FAILED_RETRYABLE" if retryable else "FAILED"

def success(st):
    st["status"]="SUCCESS";st["current_stage"]="SUCCESS";st["retryable"]=False
