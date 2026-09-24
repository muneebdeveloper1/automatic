from __future__ import annotations
import asyncio, subprocess, tempfile
from pathlib import Path
import edge_tts

class TTS:
    ORDER=("gemini","edge_tts","kokoro")
    def __init__(self,gemini,retry,voice,config):
        self.g=gemini; self.retry=retry; self.v=voice; self.config=config
        self._kokoro_pipe=None; self._kokoro_lang=None

    def settings_for(self,provider):
        if provider=="gemini":
            return {
                "provider":"gemini",
                "model":self.config.get("models",{}).get("tts",[self.v["gemini"].get("model")])[0],
                "voice":self.v["gemini"]["voice"],
                "speed":self.v.get("speed",1.0),
                "pitch":self.v.get("pitch",0),
                "volume":self.v.get("volume",1.0),
                "style":self.v.get("style")
            }
        if provider=="edge_tts":
            speed=float(self.v.get("speed",1.0)); rate=self.v["edge_tts"].get("rate")
            if self.v["edge_tts"].get("use_unified_speed",True): rate=f"{(speed-1.0)*100:+.0f}%"
            return {"provider":"edge_tts","model":None,"voice":self.v["edge_tts"]["voice"],"speed":speed,
                    "rate":rate,"pitch":self.v["edge_tts"].get("pitch",f"{int(self.v.get('pitch',0)):+d}Hz"),"volume":self.v.get("volume",1.0)}
        return {"provider":"kokoro","model":None,"voice":self.v["kokoro"]["voice"],
                "speed":float(self.v["kokoro"].get("speed",1.0)),"pitch":self.v.get("pitch",0),"volume":self.v.get("volume",1.0)}

    def _edge(self,text,out):
        async def go(tmp):
            speed=float(self.v.get("speed",1.0)); rate=self.v["edge_tts"].get("rate")
            if self.v["edge_tts"].get("use_unified_speed",True): rate=f"{(speed-1.0)*100:+.0f}%"
            pitch=self.v["edge_tts"].get("pitch",f"{int(self.v.get('pitch',0)):+d}Hz")
            c=edge_tts.Communicate(text,self.v["edge_tts"]["voice"],rate=rate,pitch=pitch)
            await c.save(str(tmp))
        with tempfile.NamedTemporaryFile(suffix=".mp3",delete=False) as f: tmp=Path(f.name)
        try:
            self.retry.run(lambda:asyncio.run(go(tmp)),"edge_tts",self.v["edge_tts"]["voice"])
            subprocess.run(["ffmpeg","-y","-i",str(tmp),"-c:a","pcm_s16le","-ar","24000","-ac","1",str(out)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        finally: tmp.unlink(missing_ok=True)

    def _kokoro(self,text,out):
        def run():
            from kokoro import KPipeline
            import numpy as np, soundfile as sf
            lang=self.v.get("language","en-US").split("-")[0]
            if self._kokoro_pipe is None or self._kokoro_lang != lang:
                self._kokoro_pipe=KPipeline(lang_code=lang); self._kokoro_lang=lang
            chunks=[audio for _,_,audio in self._kokoro_pipe(text,voice=self.v["kokoro"]["voice"],speed=float(self.v["kokoro"].get("speed",1.0)))]
            if not chunks: raise RuntimeError("Kokoro returned no audio")
            sf.write(str(out),np.concatenate(chunks),24000,subtype="PCM_16")
        self.retry.run(run,"kokoro",self.v["kokoro"]["voice"])

    def make(self,text,out,start_provider="gemini"):
        order=list(self.ORDER)
        if start_provider in order: order=order[order.index(start_provider):]
        last=None
        for p in order:
            if p=="gemini":
                try: return p,self.g.tts(text,out)
                except Exception as e: last=e
            elif p=="edge_tts" and self.v["edge_tts"].get("enabled",True):
                try: self._edge(text,out); return p,self.v["edge_tts"]["voice"]
                except Exception as e: last=e
            elif p=="kokoro" and self.v["kokoro"].get("enabled",True):
                try: self._kokoro(text,out); return p,self.v["kokoro"]["voice"]
                except Exception as e: last=e
        raise last or RuntimeError("No TTS provider succeeded")
