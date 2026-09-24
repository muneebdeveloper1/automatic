from __future__ import annotations
import hashlib, io, json, os, tempfile
from pathlib import Path, PurePosixPath
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

SCOPES=["https://www.googleapis.com/auth/drive"]
FOLDER_MIME="application/vnd.google-apps.folder"

class Drive:
    def __init__(self, credentials_json: str, retry=None, root_folder_id: str|None=None):
        info=json.loads(credentials_json)
        creds=service_account.Credentials.from_service_account_info(info,scopes=SCOPES)
        self.s=build("drive","v3",credentials=creds,cache_discovery=False)
        self.retry=retry
        self.root_id=root_folder_id or ""
        if not self.root_id: raise RuntimeError("DRIVE_ROOT_FOLDER_ID must be configured")
        self._folders={"":self.root_id}

    def _call(self, fn, provider="drive"):
        return self.retry.run(fn,provider) if self.retry else fn()

    def _escape(self, value):
        return value.replace("'","\\'")

    @staticmethod
    def local_md5(path: Path) -> str:
        h=hashlib.md5()
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
        return h.hexdigest()

    def _find_child(self,parent_id,name):
        q=f"'{parent_id}' in parents and name='{self._escape(name)}' and trashed=false"
        r=self._call(lambda:self.s.files().list(q=q,spaces="drive",fields="files(id,name,mimeType,size,modifiedTime,md5Checksum)").execute())
        files=r.get("files",[])
        if len(files)>1: raise RuntimeError(f"Drive duplicate path detected: {name} under {parent_id}")
        return files[0] if files else None

    def ensure_folder(self,name,parent_id=None):
        parent_id=parent_id or self.root_id
        key=f"{parent_id}/{name}"
        if key in self._folders:return self._folders[key]
        found=self._find_child(parent_id,name)
        if found and found["mimeType"]==FOLDER_MIME:
            fid=found["id"]
        else:
            body={"name":name,"mimeType":FOLDER_MIME,"parents":[parent_id]}
            fid=self._call(lambda:self.s.files().create(body=body,fields="id").execute())["id"]
        self._folders[key]=fid
        return fid

    def ensure_path(self,rel):
        parent=self.root_id
        parts=[p for p in PurePosixPath(rel).parts if p not in ("",".")]
        for part in parts:
            parent=self.ensure_folder(part,parent)
        return parent

    def folder_path(self,rel): return self.ensure_path(rel)

    def resolve_parent(self,rel):
        p=PurePosixPath(rel)
        parent=self.root_id
        for part in p.parts[:-1]: parent=self.ensure_folder(part,parent)
        return parent,p.name

    def find(self,rel):
        parent,name=self.resolve_parent(rel)
        return self._find_child(parent,name)

    def exists(self,rel): return self.find(rel) is not None

    def upload_file(self,local,rel,mime=None):
        local=Path(local)
        if not local.exists(): raise FileNotFoundError(local)
        parent,name=self.resolve_parent(rel)
        local_size=local.stat().st_size
        local_md5=self.local_md5(local)

        def op():
            existing=self._find_child(parent,name)
            if existing:
                # Size alone is NOT identity. Drive exposes md5Checksum for binary files.
                remote_md5=existing.get("md5Checksum")
                if int(existing.get("size") or -1)==local_size and remote_md5 and remote_md5==local_md5:
                    return existing
                media=MediaFileUpload(str(local),mimetype=mime,resumable=True,chunksize=8*1024*1024)
                return self.s.files().update(fileId=existing["id"],media_body=media,
                    body={"name":name},fields="id,name,size,md5Checksum,modifiedTime").execute()
            media=MediaFileUpload(str(local),mimetype=mime,resumable=True,chunksize=8*1024*1024)
            return self.s.files().create(body={"name":name,"parents":[parent]},media_body=media,
                fields="id,name,size,md5Checksum,modifiedTime").execute()

        return self._call(op)

    def put_json(self,obj,rel):
        parent,name=self.resolve_parent(rel)
        data=json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True).encode()
        digest=hashlib.md5(data).hexdigest()
        existing=self._find_child(parent,name)
        if existing and existing.get("md5Checksum")==digest and int(existing.get("size") or -1)==len(data):
            return existing
        media=MediaIoBaseUpload(io.BytesIO(data),mimetype="application/json",resumable=False)
        if existing:
            return self._call(lambda:self.s.files().update(fileId=existing["id"],media_body=media,
                fields="id,name,size,md5Checksum,modifiedTime").execute())
        return self._call(lambda:self.s.files().create(body={"name":name,"parents":[parent]},media_body=media,
            fields="id,name,size,md5Checksum,modifiedTime").execute())

    def download_file(self,rel,dest):
        f=self.find(rel)
        if not f: raise FileNotFoundError(rel)
        dest=Path(dest); dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=dest.with_suffix(dest.suffix+".download")
        def op():
            request=self.s.files().get_media(fileId=f["id"])
            with tmp.open("wb") as fh:
                dl=MediaIoBaseDownload(fh,request,chunksize=8*1024*1024)
                done=False
                while not done: _,done=dl.next_chunk()
            if f.get("size") is not None and tmp.stat().st_size!=int(f["size"]):
                raise IOError(f"Drive download size mismatch for {rel}")
            if f.get("md5Checksum") and self.local_md5(tmp)!=f["md5Checksum"]:
                raise IOError(f"Drive download checksum mismatch for {rel}")
            os.replace(tmp,dest)
            return dest
        try:
            return self._call(op)
        finally:
            tmp.unlink(missing_ok=True)

    def download_json(self,rel):
        with tempfile.NamedTemporaryFile(prefix="drive_",suffix=".json",delete=False) as tf:
            dest=Path(tf.name)
        try:
            self.download_file(rel,dest)
            return json.loads(dest.read_text(encoding="utf-8"))
        finally:
            dest.unlink(missing_ok=True)

    def delete(self,rel):
        f=self.find(rel)
        if f:self._call(lambda:self.s.files().delete(fileId=f["id"]).execute())

    def list_recursive(self,rel):
        start=self.find(rel)
        if not start: return []
        out=[]
        def walk(parent,prefix):
            token=None
            while True:
                q=f"'{parent}' in parents and trashed=false"
                r=self._call(lambda:self.s.files().list(q=q,spaces="drive",fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)",pageToken=token).execute())
                for f in r.get("files",[]):
                    rp=f"{prefix}/{f['name']}" if prefix else f["name"]
                    out.append((rp,f))
                    if f["mimeType"]==FOLDER_MIME: walk(f["id"],rp)
                token=r.get("nextPageToken")
                if not token: break
        walk(start["id"],"")
        return out

    def delete_tree(self,rel):
        folder=self.find(rel)
        if not folder:return
        def walk(fid):
            token=None
            while True:
                q=f"'{fid}' in parents and trashed=false"
                r=self._call(lambda:self.s.files().list(q=q,spaces="drive",fields="nextPageToken,files(id,name,mimeType)",pageToken=token).execute())
                for f in r.get("files",[]):
                    if f["mimeType"]==FOLDER_MIME:
                        walk(f["id"]);self._call(lambda fid=f["id"]:self.s.files().delete(fileId=fid).execute())
                    else:self._call(lambda fid=f["id"]:self.s.files().delete(fileId=fid).execute())
                token=r.get("nextPageToken")
                if not token:break
        walk(folder["id"]);self._call(lambda:self.s.files().delete(fileId=folder["id"]).execute())

    def list_book_states(self):
        state_folder=self.ensure_folder("STATE")
        token=None; result={}
        while True:
            q=f"'{state_folder}' in parents and name contains '.json' and trashed=false"
            r=self._call(lambda:self.s.files().list(q=q,spaces="drive",fields="nextPageToken,files(id,name,md5Checksum,size)",pageToken=token).execute())
            for f in r.get("files",[]):
                if not f["name"].endswith(".json"): continue
                # Infrastructure errors propagate. Only a confirmed not-found state is absence.
                raw=self._call(lambda fid=f["id"]:self.s.files().get_media(fileId=fid).execute())
                result[f["name"][:-5]]=json.loads(raw.decode("utf-8"))
            token=r.get("nextPageToken")
            if not token:break
        return result

    def acquire_lease(self, name="production", ttl_minutes=360):
        """Best-effort durable lease. GitHub Actions concurrency remains the primary lock.

        A stale lease is recoverable after ttl. The owner token prevents an old runner
        from deleting a newer lease during delayed cleanup.
        """
        import uuid
        from datetime import datetime, timezone, timedelta
        rel=f"STATE/LOCK_{name}.json"
        token=f"{os.getenv('GITHUB_RUN_ID','local')}-{uuid.uuid4().hex}"
        now=datetime.now(timezone.utc)
        try: current=self.download_json(rel)
        except FileNotFoundError: current=None
        if current:
            try:
                expires=datetime.fromisoformat(current["expires_at"])
                if expires > now and current.get("owner_token") != token:
                    return None
            except Exception:
                pass
        lease={"name":name,"owner_token":token,"runner":os.getenv("GITHUB_RUN_ID","local"),
               "acquired_at":now.isoformat(),"expires_at":(now+timedelta(minutes=int(ttl_minutes))).isoformat()}
        self.put_json(lease,rel)
        # Confirm our token won the last write. GitHub concurrency protects the normal path.
        confirmed=self.download_json(rel)
        return token if confirmed.get("owner_token")==token else None

    def release_lease(self,name,token):
        rel=f"STATE/LOCK_{name}.json"
        try: current=self.download_json(rel)
        except FileNotFoundError: return
        if current.get("owner_token")==token:
            self.delete(rel)

    def reconcile_work_artifacts(self,bid,st):
        """Recover artifact identity after a crash between Drive upload and state commit.

        This does not invent SHA-256 values. It records Drive's immutable file id, MD5 and
        size so the next restore can download and verify the content before marking it valid.
        """
        files=self.list_recursive(f"WORK/{bid}")
        changed=False
        for rel,meta in files:
            if rel=="state.json" or meta.get("mimeType")==FOLDER_MIME: continue
            key=rel.replace("\\","/")
            rec=st.setdefault("artifacts",{}).get(key)
            if rec is None:
                st["artifacts"][key]={
                    "path":key,"kind":"file","size":int(meta.get("size") or 0),
                    "drive_id":meta.get("id"),"drive_md5":meta.get("md5Checksum"),
                    "reconciled":True,"verified_at":utc_now()
                }
                changed=True
            else:
                if rec.get("drive_id")!=meta.get("id") or rec.get("drive_md5")!=meta.get("md5Checksum"):
                    rec["drive_id"]=meta.get("id");rec["drive_md5"]=meta.get("md5Checksum");changed=True
        return changed
