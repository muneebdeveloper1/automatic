from __future__ import annotations
import hashlib
import time
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from .retry import classify, FailureClass

SCOPES=["https://www.googleapis.com/auth/youtube.upload","https://www.googleapis.com/auth/youtube.readonly"]

class AmbiguousUploadError(RuntimeError):
    """The YouTube create request may have succeeded but its response was lost."""
    category=FailureClass.NETWORK
    ambiguous=True


def service(cid,secret,refresh):
    c=Credentials(None,refresh_token=refresh,token_uri="https://oauth2.googleapis.com/token",
                  client_id=cid,client_secret=secret,scopes=SCOPES)
    c.refresh(Request())
    return build("youtube","v3",credentials=c,cache_discovery=False)

class YouTube:
    def __init__(self,cid,secret,refresh,retry):
        self.s=service(cid,secret,refresh);self.retry=retry

    @staticmethod
    def upload_key(book_id,video_path):
        h=hashlib.sha256()
        with open(video_path,"rb") as f:
            for c in iter(lambda:f.read(1024*1024),b""):h.update(c)
        return f"AUDIOBOOK_AUTOMATION_BOOK_{book_id}_{h.hexdigest()[:20]}"

    def reconcile(self,upload_key):
        """Find an already-created video before any new insert is attempted."""
        r=self.retry.run(
            lambda:self.s.search().list(part="id,snippet",q=upload_key,forMine=True,type="video",maxResults=50).execute(),
            "youtube","search"
        )
        for item in r.get("items",[]):
            vid=item.get("id",{}).get("videoId")
            if vid:return vid
        return None

    def create_video(self,path,title,description,tags,privacy,upload_key):
        marker=f"\n\n[Production ID: {upload_key}]"
        desc=description if upload_key in description else description+marker
        body={"snippet":{"title":title,"description":desc,"tags":tags,"categoryId":"27"},
              "status":{"privacyStatus":privacy,"selfDeclaredMadeForKids":False}}
        media=MediaFileUpload(str(path),mimetype="video/mp4",chunksize=8*1024*1024,resumable=True)
        request=self.s.videos().insert(part="snippet,status",body=body,media_body=media)
        attempts=0
        while True:
            try:
                response,done=request.next_chunk()
                if done:
                    if not response or not response.get("id"):
                        raise RuntimeError("YouTube upload returned no video ID")
                    return response["id"]
                # The google client controls resumable chunk progression.
                attempts=0
            except Exception as exc:
                err=classify(exc)
                if err.category in (FailureClass.AUTH,FailureClass.INVALID,FailureClass.QUOTA):
                    raise err
                attempts += 1
                if attempts >= self.retry.attempts:
                    # Never create a second insert in this process. The create operation
                    # may have succeeded remotely. Reconcile on the next transaction step.
                    raise AmbiguousUploadError(
                        f"YouTube resumable upload became ambiguous after {attempts} retries: {err.message}"
                    ) from exc
                delay=err.retry_after if err.retry_after is not None else min(
                    self.retry.max_delay,self.retry.min_delay*(2**(attempts-1)))
                time.sleep(delay)

    def set_thumbnail(self,video_id,thumbnail):
        if not thumbnail:return
        self.retry.run(lambda:self.s.thumbnails().set(
            videoId=video_id,media_body=MediaFileUpload(str(thumbnail),mimetype="image/png")).execute(),
            "youtube","thumbnail")

    def verify(self,vid,wait_for_processing=True,max_wait_seconds=900,poll_seconds=30):
        deadline=time.time()+max_wait_seconds
        last=None
        while True:
            r=self.retry.run(lambda:self.s.videos().list(part="id,status,snippet,contentDetails,processingDetails",id=vid).execute(),"youtube","verify")
            if not r.get("items") or r["items"][0]["id"]!=vid: raise RuntimeError("YouTube verification failed")
            item=r["items"][0]; pd=item.get("processingDetails") or {}; status=pd.get("processingStatus")
            last=item
            if not wait_for_processing or status in (None,"succeeded","failed","terminated") or time.time()>=deadline: break
            time.sleep(poll_seconds)
        pd=last.get("processingDetails") or {}; status=pd.get("processingStatus")
        if status in ("failed","terminated"):
            raise RuntimeError(f"YouTube processing failed: {pd}")
        if wait_for_processing and status not in (None,"succeeded"):
            raise RuntimeError(f"YouTube processing did not finish within {max_wait_seconds}s: {status}")
        return last
