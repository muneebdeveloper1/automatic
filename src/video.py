from __future__ import annotations
import json, os, subprocess, tempfile, re, time
from pathlib import Path

def run(cmd,check=True,min_free_bytes=None,poll_seconds=5,output_path=None):
    """Run a subprocess while optionally enforcing a live disk-space guard.

    FFmpeg can consume several GB after a stage starts, so a pre-flight check alone is
    insufficient. When free space drops below the configured threshold, the process is
    terminated and the caller can persist state and retry on the next runner.
    """
    if min_free_bytes is None:
        return subprocess.run(cmd,check=check,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    import shutil, time
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    while proc.poll() is None:
        if shutil.disk_usage(Path.cwd()).free < min_free_bytes:
            proc.terminate()
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            if output_path:
                Path(output_path).unlink(missing_ok=True)
            raise RuntimeError("DISK_FULL: free disk space dropped below configured threshold during media processing")
        time.sleep(poll_seconds)
    stdout,stderr=proc.communicate()
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode,cmd,stdout,stderr)
    return subprocess.CompletedProcess(cmd,proc.returncode,stdout,stderr)

def ffprobe_json(path):
    r=run(["ffprobe","-v","error","-print_format","json","-show_format","-show_streams",str(path)])
    return json.loads(r.stdout)

def duration(path):
    info=ffprobe_json(path); return float(info["format"]["duration"])

def media_valid(path,kind,min_duration=0.5):
    try:
        if not Path(path).exists() or Path(path).stat().st_size < 1024:return False
        info=ffprobe_json(path); dur=float(info["format"]["duration"])
        if dur < min_duration:return False
        streams=info.get("streams",[])
        if kind=="audio" and not any(s.get("codec_type")=="audio" for s in streams):return False
        if kind=="video" and not any(s.get("codec_type")=="video" for s in streams):return False
        return True
    except Exception:return False

def audio_info(path):
    info=ffprobe_json(path); a=next(s for s in info["streams"] if s["codec_type"]=="audio")
    return {"duration":float(info["format"]["duration"]),"codec":a.get("codec_name"),"sample_rate":a.get("sample_rate"),"channels":a.get("channels")}

def _fps_value(value):
    try:
        if "/" in str(value):
            a,b=str(value).split("/",1)
            return float(a)/float(b)
        return float(value)
    except Exception:
        return 0.0

def video_info(path):
    info=ffprobe_json(path); v=next(s for s in info["streams"] if s["codec_type"]=="video")
    return {"duration":float(info["format"]["duration"]),"width":v.get("width"),"height":v.get("height"),"fps":_fps_value(v.get("r_frame_rate","0/1")),"has_audio":any(s.get("codec_type")=="audio" for s in info["streams"])}

def concat_audio(chunks,out,min_free_bytes=None):
    lst=out.parent/"concat.txt"
    with lst.open("w",encoding="utf-8") as f:
        for p in chunks:f.write("file '"+str(Path(p).resolve()).replace("'","'\\''")+"'\n")
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(lst),"-c:a","pcm_s16le","-ar","24000","-ac","1",str(out)],min_free_bytes=min_free_bytes,output_path=out)
    lst.unlink(missing_ok=True)

def _validate_image(path):
    from PIL import Image
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            if im.width < 2 or im.height < 2: return False
        return True
    except Exception:
        return False

def _validate_media_input(path, kind):
    if not Path(path).exists() or Path(path).stat().st_size < 1024:
        raise RuntimeError(f"Invalid {kind} input: {path}")
    if kind == "image":
        if not _validate_image(path): raise RuntimeError(f"Corrupt image input: {path}")
    elif not media_valid(path, "video" if kind == "video" else "audio", 0.5):
        raise RuntimeError(f"Corrupt {kind} input: {path}")

def build_video(bg,audio,out,cfg,cover=None,pips=None,music=None):
    _validate_media_input(bg,"video")
    _validate_media_input(audio,"audio")
    if cover and cfg["cover"]["enabled"]: _validate_media_input(cover,"image")
    pips=[p for p in (pips or []) if _validate_image(p)]
    if music: _validate_media_input(music,"audio")
    w,h=map(int,cfg["video_resolution"].split("x")); fps=cfg["video_fps"]
    inputs=["-stream_loop","-1","-i",str(bg),"-i",str(audio)]
    filters=[]; base="[0:v]"; idx=2
    if cover and cfg["cover"]["enabled"]:
        inputs += ["-loop","1","-i",str(cover)]
        filters.append(f"[{idx}:v]scale={cfg['cover']['width']}:{cfg['cover']['height']}:force_original_aspect_ratio=decrease[cover]")
        filters.append(f"{base}[cover]overlay={cfg['cover']['x']}:{cfg['cover']['y']}[vc]")
        base="[vc]";idx+=1
    pip_cfg=cfg["pip"]
    if pips and pip_cfg["enabled"]:
        for j,p in enumerate(pips):
            start=float(pip_cfg["timings"][j % len(pip_cfg["timings"])]["start_seconds"])
            dur=float(pip_cfg["timings"][j % len(pip_cfg["timings"])]["duration_seconds"])
            inputs += ["-loop","1","-i",str(p)]
            label=f"pip{idx}"
            filters.append(f"[{idx}:v]scale={pip_cfg['width']}:{pip_cfg['height']}:force_original_aspect_ratio=decrease[{label}]")
            filters.append(f"{base}[{label}]overlay={pip_cfg['x']}:{pip_cfg['y']}:enable='between(t,{start},{start+dur})'[v{idx}]")
            base=f"[v{idx}]";idx+=1
    filters.append(f"{base}scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}[vout]")
    if music:
        inputs += ["-stream_loop","-1","-i",str(music)]
        music_idx=idx
        filters.append(f"[{music_idx}:a]volume={cfg['music_volume']}[music]")
        if cfg.get("music_ducking", True):
            filters.append("[music][1:a]sidechaincompress=threshold=0.08:ratio=8:attack=20:release=400:makeup=1[musicduck]")
            filters.append("[1:a][musicduck]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]")
        else:
            filters.append("[1:a][music]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]")
    else:
        filters.append(f"[1:a]volume={cfg['narration_volume']}[aout]")
    cmd=["ffmpeg","-y",*inputs,"-filter_complex",";".join(filters),"-map","[vout]","-map","[aout]","-t",str(duration(audio)),
         "-c:v","libx264","-preset",cfg["ffmpeg_preset"],"-crf",str(cfg["ffmpeg_crf"]),
         "-c:a","aac","-b:a",cfg["audio_bitrate"],"-r",str(fps),"-movflags","+faststart",str(out)]
    minimum=int(float(cfg["resources"]["min_free_gb"])*1024**3)
    run(cmd,min_free_bytes=minimum,output_path=out)

def detect_silence(path,threshold_db=-45,min_silence=3):
    # Returns number of detected long silence intervals. Failure to analyze is conservative.
    try:
        r=run(["ffmpeg","-hide_banner","-i",str(path),"-af",f"silencedetect=noise={threshold_db}dB:d={min_silence}","-f","null","-"],check=False)
        return r.stderr.count("silence_start")
    except Exception:return 0


def black_seconds(path, sample_points=8):
    try:
        total_dur=duration(path)
        if total_dur <= 0: return 0.0
        points=[0.0]
        if sample_points > 1:
            points += [total_dur*i/(sample_points-1) for i in range(1,sample_points)]
        window=max(2.0,total_dur/(sample_points*2))
        total=0.0
        for pos in points:
            r=run(["ffmpeg","-hide_banner","-ss",str(max(0,pos)),"-i",str(path),"-t",str(window),"-vf","blackdetect=d=1:pix_th=0.98","-an","-f","null","-"],check=False)
            for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)",r.stderr):
                total += max(0.0,float(m.group(2))-float(m.group(1)))
        return min(total,total_dur)
    except Exception:return 0.0

def peak_db(path):
    try:
        r=run(["ffmpeg","-hide_banner","-i",str(path),"-af","volumedetect","-f","null","-"],check=False)
        import re
        m=re.search(r"max_volume:\s*(-?[\d.]+)\s*dB",r.stderr)
        return float(m.group(1)) if m else None
    except Exception:return None

def loudness_lufs(path):
    try:
        r=run(["ffmpeg","-hide_banner","-i",str(path),"-af","ebur128=peak=true","-f","null","-"],check=False)
        vals=re.findall(r"I:\s*(-?[\d.]+) LUFS",r.stderr)
        return float(vals[-1]) if vals else None
    except Exception:return None

def video_qc(path,cfg,audio_duration):
    if not media_valid(path,"video",min_duration=10):raise RuntimeError("Video decode/stream QC failed")
    vi=video_info(path)
    w,h=map(int,cfg["video_resolution"].split("x"))
    if (vi["width"],vi["height"])!=(w,h):raise RuntimeError(f"Video resolution mismatch: {vi['width']}x{vi['height']}")
    if abs(vi["fps"]-float(cfg["video_fps"]))>0.2:raise RuntimeError("Video FPS mismatch")
    if not vi["has_audio"]:raise RuntimeError("Video has no audio stream")
    if abs(vi["duration"]-audio_duration)>3:raise RuntimeError("Video duration mismatch")
    if black_seconds(path) > max(5.0,vi["duration"]*0.25):raise RuntimeError("Excessive black-frame duration detected")
    peak=peak_db(path)
    if peak is not None and peak >= 0.0:raise RuntimeError(f"Severe audio clipping detected: {peak} dB")
    lufs=loudness_lufs(path)
    if lufs is not None and not (-24.0 <= lufs <= -13.0): raise RuntimeError(f"Video loudness outside safe range: {lufs:.1f} LUFS")
    return vi

def audio_qc(path,min_duration=10,strict_loudness=True,check_silence=True):
    if not media_valid(path,"audio",min_duration):raise RuntimeError("Audio decode/stream QC failed")
    info=audio_info(path)
    if info["channels"] not in (1,2):raise RuntimeError("Unexpected audio channel count")
    if info["sample_rate"] not in ("24000","44100","48000"):raise RuntimeError("Unexpected sample rate")
    peak=peak_db(path)
    if peak is not None and peak >= 0.0:raise RuntimeError(f"Audio clipping detected: {peak} dB")
    if check_silence:
        silence=detect_silence(path, -45, 3)
        if silence > 0: raise RuntimeError(f"Audio contains {silence} long silence interval(s)")
    lufs=loudness_lufs(path)
    if strict_loudness and lufs is not None and not (-24.0 <= lufs <= -13.0): raise RuntimeError(f"Audio loudness outside safe range: {lufs:.1f} LUFS")
    return info
