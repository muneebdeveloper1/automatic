from __future__ import annotations
import json, re, subprocess, wave
from pathlib import Path
from google import genai
from google.genai import types
from .retry import RetryEngine

def extract_json(text):
    text=text.strip()
    if text.startswith("```"):
        text=re.sub(r"^```(?:json)?\s*","",text,flags=re.I)
        text=re.sub(r"\s*```$","",text)
    try:return json.loads(text)
    except json.JSONDecodeError: pass
    decoder=json.JSONDecoder()
    for i,c in enumerate(text):
        if c in "[{":
            try:
                obj,_=decoder.raw_decode(text[i:]);return obj
            except json.JSONDecodeError:continue
    raise ValueError("Model returned invalid JSON")

class Gemini:
    def __init__(self,key,config,voice,retry:RetryEngine):
        self.client=genai.Client(api_key=key)
        self.config=config;self.voice=voice;self.retry=retry

    def _models(self,purpose):
        models=self.config["models"].get(purpose,[])
        if isinstance(models,str):models=[models]
        if not models:raise RuntimeError(f"No Gemini models configured for {purpose}")
        return models

    def text(self,purpose,prompt):
        last=None
        for model in self._models(purpose):
            try:
                return self.retry.run(
                    lambda m=model:self.client.models.generate_content(
                        model=m,contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=self.config["generation"]["temperature"],
                            max_output_tokens=self.config["generation"]["max_output_tokens"]
                        )
                    ).text,
                    "gemini",model
                )
            except Exception as e:last=e
        raise last

    def json(self,purpose,prompt):
        # Request JSON but retain parser fallback for model/version differences.
        for attempt in range(2):
            text=self.text(purpose,prompt + "\nReturn valid JSON only. No markdown fences.")
            try:return extract_json(text)
            except Exception:
                if attempt==1:raise
        raise RuntimeError("Unreachable")

    def tts(self,text,out):
        models=self._models("tts")
        last=None
        voice=self.voice["gemini"]["voice"]
        style=self.voice.get("style","professional audiobook, calm and natural")
        for model in models:
            try:
                def call():
                    return self.client.models.generate_content(
                        model=model,
                        contents=[{"role":"user","parts":[{"text":f"{text}\n\nSpeaking style: {style}"}]}],
                        config=types.GenerateContentConfig(
                            response_modalities=["AUDIO"],
                            speech_config=types.SpeechConfig(
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                                )
                            )
                        )
                    )
                response=self.retry.run(call,"gemini_tts",model)
                data=None; mime="audio/L16;rate=24000"
                for cand in getattr(response,"candidates",[]) or []:
                    for part in getattr(getattr(cand,"content",None),"parts",[]) or []:
                        inline=getattr(part,"inline_data",None)
                        if inline:
                            data=inline.data;mime=getattr(inline,"mime_type",mime);break
                    if data:break
                if not data:raise RuntimeError("Gemini TTS returned no audio bytes")
                tmp=Path(str(out)+".raw")
                tmp.write_bytes(data)
                rate=24000
                m=re.search(r"rate[=/:](\d+)",mime or "")
                if m:rate=int(m.group(1))
                subprocess.run(["ffmpeg","-y","-f","s16le","-ar",str(rate),"-ac","1","-i",str(tmp),
                                "-c:a","pcm_s16le","-ar","24000","-ac","1",str(out)],
                               check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                tmp.unlink(missing_ok=True)
                return model
            except Exception as e:last=e
        raise last
