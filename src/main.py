from __future__ import annotations
import json, logging, os, re, shutil, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, CONFIG, VOICE, secret_config, path_from_config
from .drive import Drive
from .gemini import Gemini
from .logging_utils import log, ctx, stage
from .retry import RetryEngine, FailureClass, ClassifiedError, classify
from .state import default, load_drive, persist, record_artifact, artifact_valid, set_stage, failure, success, utc_now
from .tts import TTS
from .video import concat_audio, build_video, media_valid, duration, audio_qc, video_qc
from .thumbnail import make
from .youtube import YouTube, AmbiguousUploadError

def topics():
    result=[]
    p=ROOT/"input/topics.txt"
    if not p.exists():raise FileNotFoundError("input/topics.txt")
    for line in p.read_text(encoding="utf-8").splitlines():
        if "|" not in line:continue
        bid,title=line.split("|",1);bid=bid.strip();title=title.strip()
        if bid.isdigit() and title:result.append((bid,title))
    if len({x[0] for x in result}) != len(result):raise RuntimeError("Duplicate topic IDs")
    return sorted(result,key=lambda x:int(x[0]))

def work_dir(bid):return ROOT/"runtime"/bid

def free_bytes():
    return shutil.disk_usage(ROOT).free

def check_disk():
    minimum=int(CONFIG["resources"]["min_free_gb"]*1024**3)
    if free_bytes()<minimum:raise ClassifiedError(FailureClass.DISK,f"Free disk below {CONFIG['resources']['min_free_gb']} GB")

def clean_local_temp(w):
    for p in w.rglob("*"):
        if p.is_file() and (p.suffix in (".raw",".tmp") or p.name in ("concat.txt","scheduler.tmp")):
            p.unlink(missing_ok=True)

def read_music():
    p=ROOT/"input/music.txt"
    if not p.exists():return None
    for line in p.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#"):continue
        candidate=Path(line)
        if not candidate.is_absolute():candidate=ROOT/candidate
        if candidate.exists():return candidate
    return None

def scheduler_allowed(drive):
    """Gate scheduled *new work* without blocking recovery.

    A daily marker is written only after a new book is actually selected. A retryable
    incomplete book is always eligible on a scheduled run, even outside the nominal
    daily window, so a failed 04:00 production can recover on the next hourly trigger.
    """
    if os.getenv("GITHUB_EVENT_NAME")!="schedule": return True
    states=drive.list_book_states()
    active=[st for st in states.values() if st.get("book_id") and (
        st.get("status")=="FAILED_RETRYABLE" or
        (st.get("current_stage") not in ("TOPIC_SELECTED","QUEUED","SUCCESS") and st.get("status") not in ("FAILED","SUCCESS"))
    )]
    if active:
        log.info("recovery run allowed; active retryable job exists")
        return True

    tz=ZoneInfo(CONFIG["timezone"]); now=datetime.now(tz)
    hh,mm=map(int,CONFIG["daily_run_time"].split(":"))
    target=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    window=int(CONFIG["schedule_window_minutes"])
    if now < target or now > target+timedelta(minutes=window):
        log.info("outside new-book schedule window local=%s target=%s",now.isoformat(),target.isoformat())
        return False
    return True

def consume_daily_slot(drive,bid):
    tz=ZoneInfo(CONFIG["timezone"]); now=datetime.now(tz); date=now.date().isoformat()
    rel="STATE/scheduler.json"
    try: marker=drive.download_json(rel)
    except FileNotFoundError: marker={}
    if marker.get("last_run_date")==date and marker.get("book_id")!=bid:
        return False
    marker={"last_run_date":date,"book_id":bid,"started_at":utc_now()}
    drive.put_json(marker,rel)

def load_state(drive,bid,topic,w):
    st=load_drive(drive,bid)
    if st is None:st=default(bid,topic)
    st["topic"]=topic
    # Crash recovery: reconcile Drive artifacts that may have been uploaded before
    # the corresponding state commit completed.
    if drive.reconcile_work_artifacts(bid,st):
        log.info("reconciled orphaned Drive artifacts into state manifest")
    local=w/"state.json";persist(local,drive,st)
    return st

def restore(drive,st,w,relpaths):
    for rel in relpaths:
        if not rel:continue
        local=w/rel
        if local.exists():continue
        try:drive.download_file(f"WORK/{st['book_id']}/{rel}",local)
        except FileNotFoundError:pass

def persist_artifact(drive,st,w,rel,kind="file",provider=None,model=None,extra=None):
    """Upload artifact first, then commit its remote identity into state.

    If the runner dies between these operations, load_state() reconciles the Drive
    artifact on the next run, preventing unnecessary regeneration.
    """
    local=w/rel
    record_artifact(st,rel,local,kind,provider,model,extra)
    remote=drive.upload_file(local,f"WORK/{st['book_id']}/{rel}")
    rec=st["artifacts"][rel]
    rec["drive_id"]=remote.get("id")
    rec["drive_md5"]=remote.get("md5Checksum")
    rec["drive_size"]=int(remote.get("size") or local.stat().st_size)
    # State is committed only after the remote artifact is durable.
    drive.put_json(st,f"WORK/{st['book_id']}/state.json")
    drive.put_json(st,f"STATE/{st['book_id']}.json")

def validate_text(path,min_words=20):
    text=Path(path).read_text(encoding="utf-8").strip()
    if len(text.split())<min_words:raise RuntimeError(f"Text QC failed: {path}")
    if re.search(r"\b(TODO|TBD|PLACEHOLDER|INSERT\s+.*HERE)\b",text,re.I):raise RuntimeError(f"Unfinished placeholder in {path}")
    if re.search(r"^\s*(as an ai|i cannot|i can't|i’m an ai|i am an ai)\b",text,re.I):raise RuntimeError(f"Model refusal leaked into {path}")
    return text

def outline_qc(outline):
    if not isinstance(outline,dict) or not isinstance(outline.get("chapters"),list) or not outline["chapters"]:
        raise RuntimeError("Outline QC failed")
    nums=[]
    for c in outline["chapters"]:
        if not isinstance(c,dict): raise RuntimeError("Outline chapter must be an object")
        try: nums.append(int(c.get("number")))
        except (TypeError,ValueError): raise RuntimeError("Outline chapter number is invalid")
        for k in ("title","objective","approx_minutes"):
            if not c.get(k): raise RuntimeError(f"Outline missing {k}")
        try:
            if float(c["approx_minutes"]) <= 0: raise RuntimeError("Outline chapter duration must be positive")
        except (TypeError,ValueError): raise RuntimeError("Outline chapter duration is invalid")
    if nums != list(range(1,len(nums)+1)): raise RuntimeError("Chapter numbering is not contiguous")

def continuity_fallback(text, number, title):
    paragraphs=[p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return {"chapter":number,"title":title,"summary":" ".join(paragraphs[:2])[:900],
            "key_points":[p[:280] for p in paragraphs[:3]],"ending":" ".join(paragraphs[-2:])[:700] if paragraphs else ""}

def continuity_prompt(topic, chapter, text):
    return f"""Create a compact continuity record for chapter {chapter['number']} titled "{chapter['title']}" of an original audiobook about "{topic}".
Return JSON only with: summary (string), key_points (array of 3-6 short strings), concepts_introduced (array), examples_used (array), unresolved_threads (array), transition_to_next (string).
Do not invent anything; use only the supplied chapter.
CHAPTER:
{text}"""

def load_continuity(drive,st,w,n,ch):
    rel=f"chapters/{n:02d}.continuity.json"; f=w/rel
    restore(drive,st,w,[rel])
    if artifact_valid(st,rel,f,80):
        try:return json.loads(f.read_text(encoding="utf-8"))
        except Exception:pass
    return None

def script_prompts(topic,outline,kind,chapter=None,previous_summaries=None):
    if kind=="outline":
        return f"""Create an original educational audiobook architecture for "{topic}".
Target narration: {CONFIG['script_minutes_min']}-{CONFIG['script_minutes_max']} minutes at about {CONFIG['target_wpm']} words/minute.
Return JSON only: concept, audience, central_transformation, chapters.
chapters must contain number, title, objective, approx_minutes. Create a logical progression.
Do not invent statistics or quotations."""
    if kind=="intro":
        return f"""Write the introduction to an original educational audiobook about "{topic}".
Use a strong hook, tension, relatable situation and curiosity without fabricated statistics, quotations or citations.
Natural spoken English. No headings, notes or meta commentary. Target 900-1200 words. Return narration only."""
    summary=json.dumps(previous_summaries or [],ensure_ascii=False)[:12000]
    return f"""Write chapter {chapter['number']} titled "{chapter['title']}" for an original audiobook about "{topic}".
Objective: {chapter['objective']}. Target about {chapter['approx_minutes']} minutes at {CONFIG['target_wpm']} WPM.
This is an original educational work, not a reproduction of the source book.
Previous chapter continuity notes:
{summary or "No previous notes available."}
Do not repeat prior chapters. Use smooth transitions and concrete explanations. Do not fabricate quotations, statistics or citations.
Return narration only, no meta commentary."""

def metadata_prompt(topic):
    return f"""Create accurate YouTube metadata for an original educational audiobook about "{topic}".
Return JSON only with title, description, hashtags, tags. hashtags and tags must be arrays.
Do not claim to be the original author/book unless that is true. Do not make deceptive claims."""

def choose_book(drive):
    state_map=drive.list_book_states()
    topic_map=dict(topics())
    # Recovery always wins: resume the earliest incomplete/retryable book.
    for bid,topic in sorted(topic_map.items(), key=lambda x:int(x[0])):
        st=state_map.get(bid)
        if st and st.get("status") not in ("SUCCESS","FAILED") and st.get("retryable",True):
            return bid,topic
    # Only choose a new book when today's slot has not already been consumed.
    tz=ZoneInfo(CONFIG["timezone"]); today=datetime.now(tz).date().isoformat()
    marker=state_map.get("scheduler",{})
    if marker.get("last_run_date")==today:
        return None
    for bid,topic in sorted(topic_map.items(), key=lambda x:int(x[0])):
        if state_map.get(bid) is None:
            return bid,topic
    return None

def run_book(bid,topic,drive,gem,tts,yt,retry):
    w=work_dir(bid);w.mkdir(parents=True,exist_ok=True)
    for d in ("chapters","script","audio","video","thumbnail"): (w/d).mkdir(exist_ok=True)
    st=load_state(drive,bid,topic,w)
    ctx.set(book_id=bid,book_title=topic,stage=st["current_stage"])
    check_disk()

    # Recover only the inputs required by the current state. Generated artifacts live on Drive.
    stage_name=st["current_stage"]
    if stage_name in ("OUTLINE_COMPLETE","INTRO_COMPLETE","TOPIC_SELECTED","QUEUED"):
        restore(drive,st,w,["outline.json"])
    elif "CHAPTER" in stage_name or stage_name=="CHAPTERS_COMPLETE":
        restore(drive,st,w,["outline.json"]+[f"chapters/{i:02d}.txt" for i in range(0,st.get("chapter_count",0)+1)])
    elif stage_name in ("SCRIPT_COMPLETE","TTS_COMPLETE","AUDIO_COMPLETE"):
        restore(drive,st,w,["outline.json","script/final_script.txt"]+[f"audio/{i:04d}.wav" for i in range(1,st.get("total_chunks",0)+1)])
    elif stage_name in ("VIDEO_COMPLETE","THUMBNAIL_COMPLETE","METADATA_COMPLETE","UPLOAD_STARTED","UPLOAD_VERIFIED"):
        restore(drive,st,w,["audio/final_audio.wav","video/final.mp4","thumbnail/final.png","metadata.json"])
    else:
        restore(drive,st,w,["outline.json","script/final_script.txt","audio/final_audio.wav","video/final.mp4","thumbnail/final.png","metadata.json"])

    publication_committed=False
    try:
        # OUTLINE
        with stage(bid,topic,"OUTLINE"):
            outline_path=w/"outline.json"
            if not artifact_valid(st,"outline.json",outline_path,100):
                outline=gem.json("script",script_prompts(topic,None,"outline"));outline_qc(outline)
                outline_path.write_text(json.dumps(outline,indent=2,ensure_ascii=False),encoding="utf-8")
                persist_artifact(drive,st,w,"outline.json")
            else:outline=json.loads(outline_path.read_text(encoding="utf-8"));outline_qc(outline)
            st["chapter_count"]=len(outline["chapters"]);set_stage(st,"OUTLINE_COMPLETE");persist(w/"state.json",drive,st)

        # INTRO
        with stage(bid,topic,"INTRO"):
            intro=w/"chapters/00_intro.txt"
            if not artifact_valid(st,"chapters/00_intro.txt",intro,200):
                intro.write_text(gem.text("script",script_prompts(topic,outline,"intro")),encoding="utf-8")
                validate_text(intro,200);persist_artifact(drive,st,w,"chapters/00_intro.txt")
            set_stage(st,"INTRO_COMPLETE");persist(w/"state.json",drive,st)

        # CHAPTERS
        summaries=[]
        restore(drive,st,w,["outline.json","chapters/00_intro.txt"])
        for ch in outline["chapters"]:
            n=int(ch["number"]); rel=f"chapters/{n:02d}.txt"; f=w/rel
            if artifact_valid(st,rel,f,300):
                text=validate_text(f,300)
                cont=load_continuity(drive,st,w,n,ch)
                if not cont:
                    cont=continuity_fallback(text,n,ch["title"])
                    cf=w/f"chapters/{n:02d}.continuity.json"; cf.write_text(json.dumps(cont,indent=2,ensure_ascii=False),encoding="utf-8")
                    persist_artifact(drive,st,w,str(cf.relative_to(w)),kind="json",extra={"chapter":n,"type":"continuity"})
                summaries.append(cont); st["last_completed_chapter"]=max(st["last_completed_chapter"],n); continue
            text=gem.text("script",script_prompts(topic,outline,"chapter",ch,summaries))
            f.write_text(text.strip(),encoding="utf-8"); validate_text(f,300)
            persist_artifact(drive,st,w,rel,extra={"chapter":n})
            if CONFIG.get("continuity",{}).get("enabled",True):
                try: cont=gem.json("script",continuity_prompt(topic,ch,text))
                except Exception: cont=continuity_fallback(text,n,ch["title"])
                cf=w/f"chapters/{n:02d}.continuity.json"; cf.write_text(json.dumps(cont,indent=2,ensure_ascii=False),encoding="utf-8")
                persist_artifact(drive,st,w,str(cf.relative_to(w)),kind="json",extra={"chapter":n,"type":"continuity"})
            else: cont=continuity_fallback(text,n,ch["title"])
            st["last_completed_chapter"]=n; set_stage(st,f"CHAPTER_{n:02d}_COMPLETE"); persist(w/"state.json",drive,st)
            summaries.append(cont)
        set_stage(st,"CHAPTERS_COMPLETE");persist(w/"state.json",drive,st)

        # FINAL SCRIPT
        with stage(bid,topic,"FINAL_SCRIPT"):
            final=w/"script/final_script.txt"
            ordered=[w/"chapters/00_intro.txt"]+[w/f"chapters/{int(c['number']):02d}.txt" for c in outline["chapters"]]
            for f in ordered:restore(drive,st,w,[str(f.relative_to(w))])
            if not artifact_valid(st,"script/final_script.txt",final,1000):
                final.write_text("\n\n".join(validate_text(f,100) for f in ordered),encoding="utf-8")
                words=len(final.read_text(encoding="utf-8").split())
                target_min=CONFIG["script_minutes_min"]*CONFIG["target_wpm"]
                target_max=CONFIG["script_minutes_max"]*CONFIG["target_wpm"]
                if not (target_min <= words <= target_max):
                    raise RuntimeError(f"Script length {words} outside target {target_min}-{target_max} words")
                persist_artifact(drive,st,w,"script/final_script.txt")
            set_stage(st,"SCRIPT_COMPLETE");persist(w/"state.json",drive,st)

        # TTS
        with stage(bid,topic,"TTS"):
            check_disk();final=w/"script/final_script.txt";restore(drive,st,w,["script/final_script.txt"])
            words=final.read_text(encoding="utf-8").split()
            chunk_words=int(CONFIG["tts"]["chunk_words"])
            chunks=[" ".join(words[i:i+chunk_words]) for i in range(0,len(words),chunk_words)]
            st["total_chunks"]=len(chunks);persist(w/"state.json",drive,st)
            current_provider=st.get("last_tts_provider","gemini")
            for i,text in enumerate(chunks,1):
                rel=f"audio/{i:04d}.wav";out=w/rel
                if artifact_valid(st,rel,out,2048,media=True):
                    continue
                start=current_provider if CONFIG["tts"]["allow_provider_switch_within_book"] else "gemini"
                provider,voice_used=tts.make(text,out,start)
                audio_qc(out,1,strict_loudness=False,check_silence=False)
                effective=tts.settings_for(provider)
                if provider=="gemini":
                    effective["model"]=voice_used
                    effective["voice"]=VOICE["gemini"]["voice"]
                else:
                    effective["voice"]=voice_used
                st["tts_chunks"][str(i)]={**effective,"provider":provider,"status":"success","completed_at":utc_now()}
                st["completed_chunks"]=sorted(set(st["completed_chunks"]+[i]))
                st["last_tts_provider"]=provider
                persist_artifact(drive,st,w,rel,provider=provider,model=effective.get("model"),extra={"chunk":i,"tts_settings":effective})
                persist(w/"state.json",drive,st)
            set_stage(st,"TTS_COMPLETE");persist(w/"state.json",drive,st)

        # FINAL AUDIO
        with stage(bid,topic,"FINAL_AUDIO"):
            check_disk()
            chunks=[w/f"audio/{i:04d}.wav" for i in range(1,st["total_chunks"]+1)]
            for f in chunks:restore(drive,st,w,[str(f.relative_to(w))])
            audio=w/"audio/final_audio.wav"
            if not artifact_valid(st,"audio/final_audio.wav",audio,4096,media=True):
                concat_audio(chunks,audio,min_free_bytes=int(float(CONFIG["resources"]["min_free_gb"])*1024**3));audio_qc(audio,10);persist_artifact(drive,st,w,"audio/final_audio.wav")
            else:audio_qc(audio,10)
            st["audio_duration"]=duration(audio);set_stage(st,"AUDIO_COMPLETE");persist(w/"state.json",drive,st)

        # VIDEO
        with stage(bid,topic,"VIDEO"):
            check_disk();audio=w/"audio/final_audio.wav";restore(drive,st,w,["audio/final_audio.wav"])
            bg=path_from_config(CONFIG["assets"]["background_video"])
            if not bg.exists():raise FileNotFoundError(str(bg))
            cover=path_from_config(CONFIG["assets"]["covers_dir"])/f"{bid}.png"
            cover=cover if cover.exists() else None
            pips=sorted([p for p in path_from_config(CONFIG["assets"]["pip_dir"]).glob("*") if p.is_file()])[:int(CONFIG["pip"]["max_images"])]
            music=read_music()
            if CONFIG["music"]["required"] and music is None:raise FileNotFoundError("Required music not found")
            video=w/"video/final.mp4"
            if not artifact_valid(st,"video/final.mp4",video,1024*1024,media=True):
                build_video(bg,audio,video,CONFIG,cover,pips,music)
                video_qc(video,CONFIG,st["audio_duration"])
                persist_artifact(drive,st,w,"video/final.mp4")
            else:video_qc(video,CONFIG,st["audio_duration"])
            set_stage(st,"VIDEO_COMPLETE");persist(w/"state.json",drive,st)

        # THUMBNAIL
        with stage(bid,topic,"THUMBNAIL"):
            template=path_from_config(CONFIG["assets"]["thumbnail_template"])
            if not template.exists():raise FileNotFoundError(str(template))
            cover=path_from_config(CONFIG["assets"]["covers_dir"])/f"{bid}.png"
            cover=cover if cover.exists() else None
            thumb=w/"thumbnail/final.png"
            if not artifact_valid(st,"thumbnail/final.png",thumb,1024):
                hook=gem.text("metadata",f'Give exactly one short YouTube thumbnail hook for "{topic}". Maximum 5 words. Return only the hook.')
                make(template,cover,hook,thumb,CONFIG["thumbnail"])
                persist_artifact(drive,st,w,"thumbnail/final.png")
            set_stage(st,"THUMBNAIL_COMPLETE");persist(w/"state.json",drive,st)

        # METADATA
        with stage(bid,topic,"METADATA"):
            meta=w/"metadata.json"
            if not artifact_valid(st,"metadata.json",meta,100):
                m=gem.json("metadata",metadata_prompt(topic))
                if not isinstance(m.get("title"),str) or not m["title"].strip():raise RuntimeError("Metadata title missing")
                if not isinstance(m.get("description"),str) or not m["description"].strip():raise RuntimeError("Metadata description missing")
                if not isinstance(m.get("hashtags"),list) or not m["hashtags"]:raise RuntimeError("Metadata hashtags missing")
                if not isinstance(m.get("tags",[]),list):raise RuntimeError("Metadata tags must be a list")
                meta.write_text(json.dumps(m,indent=2,ensure_ascii=False),encoding="utf-8")
                persist_artifact(drive,st,w,"metadata.json")
            set_stage(st,"METADATA_COMPLETE");persist(w/"state.json",drive,st)

        # UPLOAD: a durable transaction protects the external side effect.
        # The video ID is persisted immediately after creation, before thumbnail/verification.
        with stage(bid,topic,"YOUTUBE_UPLOAD"):
            video=w/"video/final.mp4";thumb=w/"thumbnail/final.png";meta=w/"metadata.json"
            restore(drive,st,w,["video/final.mp4","thumbnail/final.png","metadata.json"])
            m=json.loads(meta.read_text(encoding="utf-8"))
            upload_key=st["youtube"].get("upload_key") or yt.upload_key(bid,video)
            st["youtube"]["upload_key"]=upload_key
            st["youtube"]["status"]="UPLOAD_PENDING"
            st["youtube"]["transaction_started_at"]=st["youtube"].get("transaction_started_at") or utc_now()
            set_stage(st,"UPLOAD_STARTED");persist(w/"state.json",drive,st)

            vid=st["youtube"].get("video_id")
            if not vid:
                # Reconcile first. Never blindly re-run videos.insert after an ambiguous response.
                vid=yt.reconcile(upload_key)
                if not vid:
                    try:
                        vid=yt.create_video(video,m["title"],m["description"],m.get("tags",[]),
                                            CONFIG["youtube_privacy"],upload_key)
                    except AmbiguousUploadError as exc:
                        st["youtube"]["status"]="UPLOAD_AMBIGUOUS"
                        st["youtube"]["ambiguous_at"]=utc_now()
                        persist(w/"state.json",drive,st)
                        raise
                st["youtube"]["video_id"]=vid
                st["youtube"]["uploaded_at"]=st["youtube"].get("uploaded_at") or utc_now()
                st["youtube"]["status"]="VIDEO_CREATED"
                # Critical checkpoint: video ID is durable before thumbnail or verification.
                persist(w/"state.json",drive,st)

            yt.set_thumbnail(vid,thumb)
            verified=yt.verify(vid,wait_for_processing=CONFIG.get("youtube",{}).get("wait_for_processing",True),max_wait_seconds=int(CONFIG.get("youtube",{}).get("processing_wait_seconds",900)),poll_seconds=int(CONFIG.get("youtube",{}).get("processing_poll_seconds",30)))
            st["youtube"]["processing_status"]=(verified.get("processingDetails") or {}).get("processingStatus")
            st["youtube"]["status"]="VERIFIED"
            set_stage(st,"UPLOAD_VERIFIED");persist(w/"state.json",drive,st)

        # SUCCESS transaction
        with stage(bid,topic,"SUCCESS"):
            success(st);persist(w/"state.json",drive,st)
            yjson={"book_id":bid,"video_id":st["youtube"]["video_id"],"uploaded_at":st["youtube"]["uploaded_at"],"upload_key":st["youtube"]["upload_key"]}
            success_state=drive.put_json(st,f"SUCCESS/{bid}/state.json")
            success_meta=drive.put_json(json.loads((w/"metadata.json").read_text(encoding="utf-8")),f"SUCCESS/{bid}/metadata.json")
            success_yt=drive.put_json(yjson,f"SUCCESS/{bid}/youtube.json")
            publication_committed=True
            # Destructive cleanup is allowed only after all permanent records are readable and valid.
            permanent_ok=all(drive.exists(rel) for rel in (f"SUCCESS/{bid}/state.json",f"SUCCESS/{bid}/metadata.json",f"SUCCESS/{bid}/youtube.json"))
            if not permanent_ok: raise RuntimeError(f"SUCCESS record commit verification failed for book {bid}")
            # From this point the publication is permanently successful. Cleanup failures
            # must never downgrade SUCCESS into FAILED; they are safe to finish next run.
            if CONFIG["cleanup"]["delete_work_after_success"]:
                try:
                    drive.delete_tree(f"WORK/{bid}")
                    if drive.exists(f"WORK/{bid}"): log.warning("WORK cleanup deferred book=%s",bid)
                except Exception as exc:
                    log.warning("WORK cleanup deferred book=%s error=%s",bid,str(exc)[:240])
            shutil.rmtree(w,ignore_errors=True)
            log.info("SUCCESS youtube_video_id=%s",st["youtube"]["video_id"])
    except ClassifiedError as e:
        # Quota and disk errors remain retryable across scheduled runs.
        retryable=e.category in (FailureClass.QUOTA,FailureClass.RATE_LIMIT,FailureClass.NETWORK,FailureClass.SERVER,FailureClass.TEMPORARY,FailureClass.DISK)
        failure(st,st.get("current_stage","UNKNOWN"),e,retryable)
        persist(w/"state.json",drive,st)
        drive.put_json(st,f"FAILED/{bid}/state.json")
        raise
    except Exception as e:
        if publication_committed:
            log.warning("publication already committed; cleanup/reconciliation deferred book=%s error=%s",bid,str(e)[:240])
            return
        ce=classify(e)
        retryable=ce.category not in (FailureClass.AUTH,FailureClass.INVALID,FailureClass.MISSING,FailureClass.CORRUPT,FailureClass.PERMANENT)
        failure(st,st.get("current_stage","UNKNOWN"),ce,retryable)
        persist(w/"state.json",drive,st)
        drive.put_json(st,f"FAILED/{bid}/state.json")
        (w/"error.log").write_text(str(ce),encoding="utf-8")
        drive.upload_file(w/"error.log",f"FAILED/{bid}/error.log")
        if (w/"metadata.json").exists():
            drive.upload_file(w/"metadata.json",f"FAILED/{bid}/metadata.json")
        if not retryable:
            drive.delete_tree(f"WORK/{bid}")
            shutil.rmtree(w,ignore_errors=True)
        raise

def reconcile_success_cleanup(drive):
    """Finish cleanup left behind by a runner crash after SUCCESS was committed."""
    try:
        entries=drive.list_recursive("SUCCESS")
    except Exception:
        return
    books=set()
    for rel,_ in entries:
        parts=rel.split("/")
        if len(parts)>=2: books.add(parts[0])
    for bid in books:
        try:
            if not (drive.exists(f"SUCCESS/{bid}/state.json") and drive.exists(f"SUCCESS/{bid}/metadata.json") and drive.exists(f"SUCCESS/{bid}/youtube.json")):
                continue
            if CONFIG["cleanup"]["delete_work_after_success"] and drive.exists(f"WORK/{bid}"):
                drive.delete_tree(f"WORK/{bid}")
        except Exception as exc:
            log.warning("success cleanup deferred book=%s error=%s",bid,str(exc)[:240])

def main():
    retry=RetryEngine(CONFIG["retry"]["max_attempts"],CONFIG["retry"]["min_delay_seconds"],CONFIG["retry"]["max_delay_seconds"])
    sec=secret_config()
    drive=Drive(sec["google_drive_credentials"],retry,os.getenv("DRIVE_ROOT_FOLDER_ID") or CONFIG["drive"]["root_folder_id"])
    drive.ensure_folder("STATE");drive.ensure_folder("WORK");drive.ensure_folder("SUCCESS");drive.ensure_folder("FAILED")
    lease=drive.acquire_lease("production",CONFIG.get("resources",{}).get("lock_ttl_minutes",360))
    if not lease:
        log.warning("another production runner holds the durable lease; exiting safely")
        return
    try:
        if not scheduler_allowed(drive):return
        selected=choose_book(drive)
        if not selected:
            log.info("No eligible books")
            return
        bid,topic=selected
        existing=drive.list_book_states().get(bid)
        if existing is None:
            # Persist the initial state before consuming the daily slot. If the runner
            # dies immediately afterwards, the next scheduled run sees a real recovery job.
            w=work_dir(bid);w.mkdir(parents=True,exist_ok=True)
            initial=default(bid,topic)
            persist(w/"state.json",drive,initial)
            if not consume_daily_slot(drive,bid):
                # Another book already consumed today's new-production slot. Keep the
                # queued state; tomorrow it becomes eligible.
                log.info("daily production slot already consumed; deferring new book %s",bid)
                return
        gem=Gemini(sec["gemini_api_key"],CONFIG,VOICE,retry)
        tts=TTS(gem,retry,VOICE,CONFIG)
        yt=YouTube(sec["youtube_client_id"],sec["youtube_client_secret"],sec["youtube_refresh_token"],retry)
        try:
            run_book(bid,topic,drive,gem,tts,yt,retry)
        finally:
            clean_local_temp(work_dir(bid))
    finally:
        drive.release_lease("production",lease)

if __name__=="__main__":main()
